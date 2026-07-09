# shellcheck shell=bash
computer_facade_smoke() {
  local python
  if ! python="$(runtime_settings_python)"; then
    echo "No Python runtime is available for computer facade smoke." >&2
    return 1
  fi
  LITELLM_SMOKE_BASE_URL="http://127.0.0.1:$PORT" \
  LITELLM_SMOKE_MASTER_KEY="$MASTER_KEY" \
  LITELLM_SMOKE_MODEL="${LITELLM_MENU_SMOKE_MODEL:-}" \
  LITELLM_SMOKE_INCLUDE_WEB="${LITELLM_MENU_SMOKE_INCLUDE_WEB:-0}" \
  LITELLM_SMOKE_INCLUDE_IMAGE="${LITELLM_MENU_SMOKE_INCLUDE_IMAGE:-0}" \
  "$python" - <<'PY'
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


base_url = os.environ["LITELLM_SMOKE_BASE_URL"].rstrip("/")
master_key = os.environ.get("LITELLM_SMOKE_MASTER_KEY", "")
model = os.environ.get("LITELLM_SMOKE_MODEL", "").strip()
include_web = os.environ.get("LITELLM_SMOKE_INCLUDE_WEB", "0").strip().lower() in {"1", "true", "yes", "on"}
include_image = os.environ.get("LITELLM_SMOKE_INCLUDE_IMAGE", "0").strip().lower() in {"1", "true", "yes", "on"}


def request_json(method: str, path: str, payload: dict | None = None) -> tuple[dict, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {master_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = response.read()
            headers = {key.lower(): value for key, value in response.headers.items()}
            text = body.decode("utf-8", errors="replace")
            parsed = json.loads(text) if text.strip() else {}
            return parsed, {
                "ok": True,
                "status": response.status,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "headers": {
                    key: headers.get(key)
                    for key in (
                        "x-litellm-model-id",
                        "x-litellm-model-api-base",
                        "x-litellm-attempted-fallbacks",
                    )
                    if headers.get(key)
                },
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {}, {
            "ok": False,
            "status": exc.code,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": body[:1000],
        }
    except Exception as exc:
        return {}, {
            "ok": False,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }


def response_summary(payload: dict, meta: dict) -> dict:
    output = payload.get("output") if isinstance(payload, dict) else None
    output_items = output if isinstance(output, list) else []
    return {
        **meta,
        "response_id": payload.get("id") if isinstance(payload, dict) else None,
        "status_text": payload.get("status") if isinstance(payload, dict) else None,
        "output_text": (payload.get("output_text") or "")[:500] if isinstance(payload, dict) else "",
        "output_types": [
            item.get("type")
            for item in output_items
            if isinstance(item, dict) and isinstance(item.get("type"), str)
        ],
        "action_types": [
            action.get("type")
            for item in output_items
            if isinstance(item, dict)
            for action in (item.get("actions") if isinstance(item.get("actions"), list) else [])
            if isinstance(action, dict) and isinstance(action.get("type"), str)
        ],
    }


report: dict[str, object] = {"base_url": base_url, "cases": {}}

health_payload, health_meta = request_json("GET", "/health/liveliness")
report["cases"]["health"] = {
    **health_meta,
    "body": health_payload if health_payload else "non-json or empty",
}

if not model:
    models_payload, models_meta = request_json("GET", "/v1/models")
    models = models_payload.get("data") if isinstance(models_payload, dict) else None
    if isinstance(models, list) and models:
        first = models[0]
        if isinstance(first, dict):
            model = str(first.get("id") or "").strip()
    report["cases"]["model_discovery"] = {**models_meta, "model": model}

if not model:
    report["ok"] = False
    report["error"] = "No model available. Set LITELLM_MENU_SMOKE_MODEL."
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(1)

report["model"] = model

text_payload, text_meta = request_json(
    "POST",
    "/v1/responses",
    {
        "model": model,
        "input": "LiteLLM Menu smoke: reply briefly with ok.",
        "max_output_tokens": 32,
    },
)
report["cases"]["ordinary_text"] = response_summary(text_payload, text_meta)

computer_payload, computer_meta = request_json(
    "POST",
    "/v1/responses",
    {
        "model": model,
        "input": "Computer facade smoke: request a screenshot only.",
        "tools": [{"type": "computer"}],
        "tool_choice": "required",
        "max_output_tokens": 64,
    },
)
report["cases"]["hosted_computer"] = response_summary(computer_payload, computer_meta)

tool_payload, tool_meta = request_json(
    "POST",
    "/v1/responses",
    {
        "model": model,
        "input": "LiteLLM Menu smoke: if tool_search is available, ask for available tools.",
        "tools": [{"type": "tool_search"}],
        "tool_choice": "auto",
        "max_output_tokens": 128,
    },
)
report["cases"]["tool_search"] = response_summary(tool_payload, tool_meta)

if include_web:
    web_payload, web_meta = request_json(
        "POST",
        "/v1/responses",
        {
            "model": model,
            "input": "LiteLLM Menu smoke: use web_search to find the OpenAI homepage URL.",
            "tools": [{"type": "web_search"}],
            "tool_choice": "auto",
            "max_output_tokens": 256,
        },
    )
    report["cases"]["web_search"] = response_summary(web_payload, web_meta)
else:
    report["cases"]["web_search"] = {"skipped": True, "reason": "Set LITELLM_MENU_SMOKE_INCLUDE_WEB=1 to run."}

if include_image:
    image_payload, image_meta = request_json(
        "POST",
        "/v1/responses",
        {
            "model": model,
            "input": "LiteLLM Menu smoke: create a tiny plain red square image.",
            "tools": [{"type": "image_generation"}],
            "tool_choice": "auto",
            "max_output_tokens": 256,
        },
    )
    report["cases"]["image_generation"] = response_summary(image_payload, image_meta)
else:
    report["cases"]["image_generation"] = {"skipped": True, "reason": "Set LITELLM_MENU_SMOKE_INCLUDE_IMAGE=1 to run."}

report["ok"] = all(
    case.get("ok", True) is not False
    for case in report["cases"].values()
    if isinstance(case, dict)
)
print(json.dumps(report, ensure_ascii=False, indent=2))
sys.exit(0 if report["ok"] else 1)
PY
}

ACTION="${1:-status}"
