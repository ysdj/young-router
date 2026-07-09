# shellcheck shell=bash
runtime_reload_fingerprint() {
  "$PYTHON" - "$RUNTIME_CONFIG" "$CALLBACK_SOURCE" "$CALLBACK_PACKAGE_DIR" <<'PY'
from __future__ import annotations

import hashlib
import json
import pathlib
import sys

try:
    import yaml
except Exception as exc:
    print(f"PyYAML is required to fingerprint runtime config: {exc}", file=sys.stderr)
    sys.exit(1)

config_path = pathlib.Path(sys.argv[1])
callback_path = pathlib.Path(sys.argv[2])
callback_package = pathlib.Path(sys.argv[3])
data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
settings = data.get("litellm_settings") if isinstance(data, dict) else {}
callbacks = settings.get("callbacks") if isinstance(settings, dict) else []
if callbacks is None:
    callbacks = []

hasher = hashlib.sha256()
hashed_files = []
for candidate in [callback_path, *sorted(callback_package.rglob("*.py"))]:
    if not candidate.is_file():
        continue
    relative = str(candidate.relative_to(callback_package.parent))
    hasher.update(relative.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(candidate.read_bytes())
    hashed_files.append(relative)

callback_sha256 = hasher.hexdigest() if hashed_files else None

print(
    json.dumps(
        {
            "callbacks": callbacks,
            "callback_sha256": callback_sha256,
            "callback_files": hashed_files,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
)
PY
}

write_runtime_reload_fingerprint() {
  local tmp
  mkdir -p "$RUNTIME_DIR"
  tmp="$(mktemp "$RUNTIME_DIR/reload-fingerprint.XXXXXX")"
  if runtime_reload_fingerprint >"$tmp"; then
    chmod 600 "$tmp" 2>/dev/null || true
    mv "$tmp" "$RUNTIME_RELOAD_FINGERPRINT"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

runtime_reload_fingerprint_changed() {
  local current previous
  if ! current="$(runtime_reload_fingerprint)"; then
    return 0
  fi
  if [[ ! -f "$RUNTIME_RELOAD_FINGERPRINT" ]]; then
    return 0
  fi
  previous="$(cat "$RUNTIME_RELOAD_FINGERPRINT" 2>/dev/null || true)"
  [[ "$current" != "$previous" ]]
}

fetch_runtime_model_info() {
  if [[ -n "$MODEL_INFO_FILE" ]]; then
    cat "$MODEL_INFO_FILE"
    return
  fi
  curl -fsS --max-time 8 -H "Authorization: Bearer $MASTER_KEY" "$MODEL_INFO_URL"
}

verify_runtime_config() {
  local tmp
  if [[ ! -f "$RUNTIME_CONFIG" ]]; then
    echo "Missing runtime config: $RUNTIME_CONFIG" >&2
    return 1
  fi

  tmp="$(mktemp "${TMPDIR:-/tmp}/litellm-model-info.XXXXXX")"
  if ! fetch_runtime_model_info >"$tmp"; then
    rm -f "$tmp"
    echo "Failed to fetch LiteLLM model info from $MODEL_INFO_URL" >&2
    return 1
  fi

  if "$PYTHON" - "$RUNTIME_CONFIG" "$tmp" <<'PY'
from __future__ import annotations

from collections import Counter, defaultdict
import json
import pathlib
import sys

try:
    import yaml
except Exception as exc:
    print(f"PyYAML is required to verify runtime routes: {exc}", file=sys.stderr)
    sys.exit(1)


def as_dict(value):
    return value if isinstance(value, dict) else {}


def as_list(value):
    return value if isinstance(value, list) else []


def text(value):
    return "" if value is None else str(value).strip()


def order(value):
    value = text(value)
    if not value:
        return ""
    try:
        return str(int(float(value)))
    except ValueError:
        return value


def model_info_rows(value):
    if isinstance(value, dict):
        if "data" in value:
            return model_info_rows(value["data"])
        if any(key in value for key in ("model_name", "litellm_params", "model_info")):
            return [value]
    if isinstance(value, list):
        rows = []
        for item in value:
            rows.extend(model_info_rows(item))
        return rows
    return []


def identity(row):
    row = as_dict(row)
    params = as_dict(row.get("litellm_params"))
    info = as_dict(row.get("model_info"))
    return json.dumps(
        {
            "model_name": text(row.get("model_name")),
            "id": text(info.get("id")),
            "provider": text(info.get("provider")),
            "api_key_name": text(info.get("api_key_name")),
            "route_key": text(info.get("route_key")),
            "model": text(params.get("model")),
            "api_base": text(params.get("api_base")).rstrip("/"),
            "order": order(params.get("order") if params.get("order") is not None else info.get("order")),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def label(row):
    row = as_dict(row)
    params = as_dict(row.get("litellm_params"))
    info = as_dict(row.get("model_info"))
    route = text(info.get("route_key"))
    if not route:
        parts = [
            text(info.get("provider")) or "(blank-provider)",
            text(params.get("model")) or "(blank-upstream)",
        ]
        key_name = text(info.get("api_key_name"))
        if key_name:
            parts.append(f"key={key_name}")
        route_order = order(params.get("order") if params.get("order") is not None else info.get("order"))
        if route_order:
            parts.append(f"order={route_order}")
        route = " / ".join(parts)
    return (
        f"{text(row.get('model_name')) or '(blank-model-name)'} "
        f"route={route} "
        f"token={text(info.get('id')) or '(blank-token)'} "
        f"api_base={text(params.get('api_base')) or '(blank-api-base)'}"
    )


config_path = pathlib.Path(sys.argv[1])
model_info_path = pathlib.Path(sys.argv[2])
config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
model_info = json.loads(model_info_path.read_text(encoding="utf-8"))

expected_rows = as_list(config.get("model_list"))
actual_rows = model_info_rows(model_info)
expected = Counter(identity(row) for row in expected_rows)
actual = Counter(identity(row) for row in actual_rows)

if expected == actual:
    print(f"Runtime routes verified: {len(actual_rows)} deployments match {config_path}")
    sys.exit(0)

expected_labels = defaultdict(list)
actual_labels = defaultdict(list)
for row in expected_rows:
    expected_labels[identity(row)].append(label(row))
for row in actual_rows:
    actual_labels[identity(row)].append(label(row))

missing = list((expected - actual).elements())
extra = list((actual - expected).elements())
print(
    f"Runtime route mismatch: /model/info has {len(actual_rows)} deployments, "
    f"but {config_path} has {len(expected_rows)} active deployments.",
    file=sys.stderr,
)
if missing:
    print("Missing from runtime:", file=sys.stderr)
    for item in missing[:20]:
        print(f"  - {expected_labels[item].pop(0)}", file=sys.stderr)
if extra:
    print("Extra in runtime:", file=sys.stderr)
    for item in extra[:20]:
        print(f"  - {actual_labels[item].pop(0)}", file=sys.stderr)
sys.exit(1)
PY
  then
    rm -f "$tmp"
    return 0
  fi

  rm -f "$tmp"
  return 1
}

wait_for_runtime_config() {
  local attempt output status
  local max_attempts
  max_attempts=$((RUNTIME_VERIFY_WAIT_SECONDS * 5))
  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    if output="$(verify_runtime_config 2>&1)"; then
      printf '%s\n' "$output"
      return 0
    fi
    status=$?
    if (( attempt == max_attempts )); then
      printf '%s\n' "$output" >&2
      return "$status"
    fi
    sleep 0.2
  done
}
