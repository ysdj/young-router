#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if (($# == 0)); then
  exec env PYTHONPATH=.:tests python3 -m unittest discover -s tests -p 'test*.py' -v
fi

targets=()
for target in "$@"; do
  if [[ "$target" == *"::"* ]]; then
    printf 'error: pytest-style selectors are not supported; use unittest module paths\n' >&2
    exit 2
  fi
  case "$target" in
    tests/*.py)
      target="${target#tests/}"
      target="${target%.py}"
      ;;
    *.py)
      target="${target%.py}"
      ;;
  esac
  target="${target//\//.}"
  targets+=("$target")
done

exec env PYTHONPATH=.:tests python3 -m unittest "${targets[@]}" -v
