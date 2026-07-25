#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

TEST_COMMAND=()
if [[ -n "${LITELLM_TEST_PYTHON:-}" ]]; then
  TEST_COMMAND=("$LITELLM_TEST_PYTHON")
elif [[ -x "${LITELLM_BUNDLED_TEST_PYTHON:-/Applications/LiteLLM Menu.app/Contents/Resources/App/runtime/bin/python}" ]] \
  && "${LITELLM_BUNDLED_TEST_PYTHON:-/Applications/LiteLLM Menu.app/Contents/Resources/App/runtime/bin/python}" -c 'import yaml, litellm' >/dev/null 2>&1; then
  TEST_COMMAND=("${LITELLM_BUNDLED_TEST_PYTHON:-/Applications/LiteLLM Menu.app/Contents/Resources/App/runtime/bin/python}")
elif command -v uv >/dev/null 2>&1; then
  TEST_COMMAND=(
    uv run --python 3.12
    --with "litellm[proxy]==$(tr -d '[:space:]' < LITELLM_VERSION)"
    --with PyYAML
    --with Pillow
    --with ddgs
    python
  )
else
  echo "A supported Python 3.11+ test runtime is required. Install uv or set LITELLM_TEST_PYTHON." >&2
  exit 1
fi

if ! "${TEST_COMMAND[@]}" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
then
  echo "LiteLLM Menu tests require Python 3.11+; set LITELLM_TEST_PYTHON to a supported interpreter." >&2
  exit 1
fi

if (($# == 0)); then
  exec env PYTHONPATH=.:tests "${TEST_COMMAND[@]}" -m unittest discover -s tests -p 'test*.py' -v
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

exec env PYTHONPATH=.:tests "${TEST_COMMAND[@]}" -m unittest "${targets[@]}" -v
