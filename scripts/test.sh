#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# Focused local test runs may intentionally reuse the installed app's bundled
# Python. Never let those imports write __pycache__ files into the signed app
# bundle, because any post-signing file invalidates the macOS code signature.
export PYTHONDONTWRITEBYTECODE=1

# Keep the shared desktop UI contract in the same gate as the Python Core.
# This check is intentionally dependency-free: CI and checkout-only test runs
# should still catch route/protocol drift before installing the RN toolchain.
if command -v node >/dev/null 2>&1 && [[ -f rn/scripts/check-contract.mjs ]]; then
  node rn/scripts/check-contract.mjs
fi

TEST_COMMAND=()
if [[ -n "${LITELLM_TEST_PYTHON:-}" ]]; then
  TEST_COMMAND=("$LITELLM_TEST_PYTHON")
  # Keep focused Core and integration tests on the selected runtime too.
  export PYTHON="$LITELLM_TEST_PYTHON"
elif [[ -x "${LITELLM_BUNDLED_TEST_PYTHON:-/Applications/LiteLLM Menu.app/Contents/Resources/Core/runtime/bin/python}" ]] \
  && "${LITELLM_BUNDLED_TEST_PYTHON:-/Applications/LiteLLM Menu.app/Contents/Resources/Core/runtime/bin/python}" -c 'import yaml, litellm' >/dev/null 2>&1; then
  TEST_COMMAND=("${LITELLM_BUNDLED_TEST_PYTHON:-/Applications/LiteLLM Menu.app/Contents/Resources/Core/runtime/bin/python}")
elif command -v uv >/dev/null 2>&1; then
  TEST_COMMAND=(
    uv run --python 3.12
    --with "litellm[proxy]==$(tr -d '[:space:]' < LITELLM_VERSION)"
    --with "fastapi==0.140.3"
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
