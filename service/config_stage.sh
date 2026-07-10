# shellcheck shell=bash

validate_config_file() {
  local path="$1"
  PYTHONPATH="$TEMPLATE_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" - "$path" <<'PY'
import pathlib
import sys

from config_editor_core.schema import _load_yaml

path = pathlib.Path(sys.argv[1])
data = _load_yaml(path)
print(f"config.yaml OK: {len(data['model_list'])} model entries")
PY
}

remove_runtime_callback_artifacts() {
  rm -f \
    "$RUNTIME_DIR/callbacks.py" \
    "$RUNTIME_DIR/litellm_hooks.py" \
    "$RUNTIME_DIR/__pycache__/callbacks.cpython-"*.pyc \
    "$RUNTIME_DIR/__pycache__/litellm_hooks.cpython-"*.pyc
}

sync_runtime_config() {
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Missing config file: $CONFIG_FILE" >&2
    return 1
  fi

  mkdir -p "$RUNTIME_DIR"
  remove_runtime_callback_artifacts
  if [[ ! -f "$CALLBACK_SOURCE" ]]; then
    echo "Missing LiteLLM callback file: $CALLBACK_SOURCE" >&2
    return 1
  fi
  if [[ ! -d "$CALLBACK_PACKAGE_DIR" ]]; then
    echo "Missing LiteLLM callback package: $CALLBACK_PACKAGE_DIR" >&2
    return 1
  fi

  if [[ -f "$RUNTIME_CONFIG" ]] && cmp -s "$CONFIG_FILE" "$RUNTIME_CONFIG"; then
    if [[ "$MIRROR_CHECKOUT_CONFIG_TO_ROOT" != "1" ]] \
      || { [[ -f "$ROOT/config.yaml" ]] && cmp -s "$CONFIG_FILE" "$ROOT/config.yaml"; }; then
      echo "config.yaml unchanged: runtime config already current"
      return 0
    fi
  fi

  PYTHONPATH="$TEMPLATE_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" - "$CONFIG_FILE" "$RUNTIME_CONFIG" "$MIRROR_CHECKOUT_CONFIG_TO_ROOT" "$ROOT/config.yaml" <<'PY'
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

from config_editor_core.schema import _load_yaml


def atomic_write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


source_path = pathlib.Path(sys.argv[1])
runtime_path = pathlib.Path(sys.argv[2])
mirror_to_root = sys.argv[3] == "1"
root_config_path = pathlib.Path(sys.argv[4])

source_text = source_path.read_text(encoding="utf-8")
data = _load_yaml(source_path)
atomic_write(runtime_path, source_text.encode("utf-8"))
if mirror_to_root and source_path.resolve() != root_config_path.resolve():
    atomic_write(root_config_path, source_text.encode("utf-8"))
    print(f"Mirrored checkout config to {root_config_path}")
print(f"config.yaml OK: {len(data['model_list'])} model entries")
PY
}
