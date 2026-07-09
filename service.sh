#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_LIB_DIR="${LITELLM_MENU_SERVICE_LIB_DIR:-$SCRIPT_DIR/service}"
export LITELLM_MENU_SCRIPT_DIR="$SCRIPT_DIR"

SERVICE_PARTS=(
  environment.sh
  runtime_settings.sh
  runtime_settings_configure.sh
  config_stage.sh
  runtime_verify.sh
  process.sh
  logs_trace.sh
  webdav.sh
  launchd_watch.sh
  smoke.sh
  dispatch.sh
)

for service_part in "${SERVICE_PARTS[@]}"
do
  # shellcheck source=/dev/null
  source "$SERVICE_LIB_DIR/$service_part"
done
